// @vitest-environment happy-dom
/**
 * Add model › Subscription signs in on the BOSS server (BOSSAPP-769, P0-4 console half; P0-3's
 * Sources badge). Codex, Claude Code and Grok are never pasted, dropped or imported: no token
 * field, no auth.json drop, no `claude setup-token`, no Replace. Sign in shows the provider's URL
 * and code, polls until signed in, and only then Connect — which sends no credential — proves it.
 * A failed proof's row offers Sign in (never Replace); the Sources badge is that row's own pill.
 */
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { signInTiming } from "../app/setup/subscription-sign-in";
import { overBudget } from "./_helpers/copy-budget";
import { click, fakeApi, flush, mountBoard, press, q, type, type Call, type FakeApi, type Mounted } from "./_helpers/setup-board";
import { board, PILLS } from "./_helpers/setup-fixtures";
import type { SetupView } from "../lib/setup-types";

const S = JSON.parse(readFileSync("tests/fixtures/sign-in-sessions.json", "utf8"));
const SOURCES = JSON.parse(readFileSync("tests/fixtures/sources-login-expired.json", "utf8"));

let mounted: Mounted | null = null;
beforeEach(() => { signInTiming.pollMs = 0; });
afterEach(() => {
  mounted?.unmount(); mounted = null; document.body.innerHTML = "";
  signInTiming.pollMs = 2000; vi.unstubAllGlobals();
});

const SUBS = ["codex", "claude_code", "grok"] as const;

/** The board with every sign-in subscription on the server but not signed in. */
function view(patch: (v: SetupView) => void = () => undefined): SetupView {
  const v = board();
  v.detected = v.detected.map((d) => SUBS.includes(d.provider as never) ? { ...d, found: "needs_sign_in" } : d);
  patch(v);
  return v;
}
/** The same board with no subscription connected yet (the fixture's Claude Code row works). */
function unconnected(): SetupView {
  return view((v) => { v.models = v.models.filter((m) => !SUBS.includes(m.provider as never)); });
}
const drawer = () => q(document.body, "su-model-drawer") as HTMLElement;
const connect = () => q(document.body, "su-model-connect") as HTMLButtonElement;
const isSignInPoll = (c: Call) => c.method === "GET" && c.url.includes("sign_in=");

/** Answers sign-in ops from a script: start → `start`, each poll → the next of `polls`. */
function script(api: FakeApi, { start, polls = [], code, cancel = S.cancelled }: {
  start: unknown; polls?: unknown[]; code?: unknown; cancel?: unknown;
}) {
  let i = 0;
  const before = api.answer;
  api.answer = (call) => {
    if (call.op === "sign_in_start") {
      return (start as { sign_in_id?: string }).sign_in_id
        ? { status: 200, body: start } : { status: 409, body: { error: "refused", refusal: start } };
    }
    if (call.op === "sign_in_code") return { status: 200, body: code };
    if (call.op === "sign_in_cancel") return { status: 200, body: cancel };
    if (isSignInPoll(call)) return { status: 200, body: polls[Math.min(i++, polls.length - 1)] };
    return before(call);
  };
}
async function settle(until: () => boolean, rounds = 12) {
  for (let i = 0; i < rounds && !until(); i += 1) await flush();
}

describe("the subscription tab asks for nothing to paste", () => {
  for (const provider of SUBS) {
    it(`${provider}: no password field, no file drop, no setup-token or auth.json; Sign in instead`, async () => {
      fakeApi(unconnected());
      mounted = await mountBoard(unconnected(), `?area=setup&add=${provider}`);
      const sheet = drawer();
      expect(sheet).not.toBeNull();
      expect(sheet.querySelectorAll('input[type="password"]').length).toBe(0);
      expect(sheet.querySelectorAll('input[type="file"]').length).toBe(0);
      expect(q(sheet, "su-copy-chip")).toBeNull();
      expect(q(sheet, "su-model-token")).toBeNull();
      expect(q(sheet, "su-model-file")).toBeNull();
      const text = sheet.textContent ?? "";
      expect(text).not.toMatch(/setup-token|auth\.json|Paste a token|Drop/);
      expect(q(sheet, "su-signin-start")?.textContent).toBe("Sign in");
      expect(connect().disabled).toBe(true);
      expect(connect().getAttribute("title")).toBe("Sign in first");
      expect(overBudget(sheet)).toEqual([]);
    });
  }
});

describe("Sign in → the URL and code → polled to signed in → Connect sends no credential", () => {
  it("shows the link (new tab) and the code, polls, enables Connect, and posts kind subscription only", async () => {
    const api = fakeApi(view());
    script(api, { start: S.waiting, polls: [S.waiting, S.waiting, S.signed_in] });
    mounted = await mountBoard(view(), "?area=setup&add=codex");
    await click(q(drawer(), "su-signin-start"));
    const start = api.calls.find((c) => c.op === "sign_in_start")!;
    expect(start.body).toMatchObject({ provider: "codex", project_id: "p" });
    await settle(() => !!q(drawer(), "su-signin-done"));
    expect(q(drawer(), "su-signin-done")!.textContent).toBe("Signed in");
    const polls = api.calls.filter(isSignInPoll);
    expect(polls.length).toBeGreaterThanOrEqual(2);
    expect(polls[0].url).toBe("/api/setup?project_id=p&sign_in=si_codex_1");
    expect(connect().disabled).toBe(false);
    await click(connect());
    const sent = api.calls.find((c) => c.op === "models")!;
    expect(sent.body).toEqual({ op: "models", project_id: "p", provider: "codex", kind: "subscription" });
    // Signed in is not live: closing after Connect cancels nothing.
    expect(api.calls.some((c) => c.op === "sign_in_cancel")).toBe(false);
  });

  it("draws the waiting state with the URL and code before it is signed in", async () => {
    const api = fakeApi(view());
    script(api, { start: S.waiting, polls: [S.waiting] });
    signInTiming.pollMs = 60_000;                          // hold it in Waiting
    mounted = await mountBoard(view(), "?area=setup&add=codex");
    await click(q(drawer(), "su-signin-start"));
    expect(q(drawer(), "su-signin")!.getAttribute("data-state")).toBe("waiting");
    const open = q(drawer(), "su-signin-open") as HTMLAnchorElement;
    expect(open.getAttribute("href")).toBe("https://auth.openai.com/codex/device");
    expect(open.getAttribute("target")).toBe("_blank");
    expect(open.getAttribute("rel")).toContain("noopener");
    const code = q(drawer(), "su-signin-code")!;
    expect(code.querySelector("code")!.textContent).toBe("ABCD-1234");
    expect(q(drawer(), "su-signin-waiting")).not.toBeNull();
    expect(connect().disabled).toBe(true);
    expect(overBudget(drawer())).toEqual([]);
  });

  it("starting has its own state before the CLI prints a URL", async () => {
    const api = fakeApi(view());
    script(api, { start: S.starting, polls: [S.starting] });
    signInTiming.pollMs = 60_000;
    mounted = await mountBoard(view(), "?area=setup&add=codex");
    await click(q(drawer(), "su-signin-start"));
    expect(q(drawer(), "su-signin")!.getAttribute("data-state")).toBe("starting");
    expect(q(drawer(), "su-signin-starting")!.textContent).toContain("Starting sign-in");
    expect(q(drawer(), "su-signin-cancel")).not.toBeNull();
  });
});

describe("a CLI that asks for a code pasted back", () => {
  it("shows one plain code box; Submit hands it to the sign-in and clears the box", async () => {
    const api = fakeApi(unconnected());
    script(api, { start: S.waiting_for_code, polls: [S.waiting_for_code],
                  code: { ...S.signed_in, sign_in_id: "si_claude_1", provider: "claude_code" } });
    signInTiming.pollMs = 60_000;
    mounted = await mountBoard(unconnected(), "?area=setup&add=claude_code");
    await click(q(drawer(), "su-signin-start"));
    const box = q(drawer(), "su-signin-code-input") as HTMLInputElement;
    // It says where the provider's code goes: the provider's page says "paste into Claude Code".
    expect(q(drawer(), "su-signin-code-help")!.textContent).toContain("paste it here");
    expect(box.getAttribute("placeholder")).toBe("Paste the code here");
    expect(q(drawer(), "su-signin-waiting")!.textContent).toContain("Waiting for the code");
    expect(box.getAttribute("type")).toBe("text");
    expect(drawer().querySelectorAll('input[type="password"]').length).toBe(0);
    expect((q(drawer(), "su-signin-code-submit") as HTMLButtonElement).disabled).toBe(true);
    await type(box, "auth-code-xyz");
    await flush();
    await click(q(drawer(), "su-signin-code-submit"));
    const sent = api.calls.find((c) => c.op === "sign_in_code")!;
    expect(sent.body).toMatchObject({ sign_in_id: "si_claude_1", code: "auth-code-xyz" });
    expect(q(drawer(), "su-signin-done")).not.toBeNull();
    expect(connect().disabled).toBe(false);
    await click(connect());
    expect(api.calls.find((c) => c.op === "models")!.body)
      .toEqual({ op: "models", project_id: "p", provider: "claude_code", kind: "subscription" });
  });
});

describe("a stopped sign-in says its headline and fix", () => {
  for (const state of ["failed", "expired"] as const) {
    it(`${state}: headline pill, the fix beside it, Try again; Connect stays off`, async () => {
      const api = fakeApi(view());
      script(api, { start: S.waiting, polls: [S[state]] });
      mounted = await mountBoard(view(), "?area=setup&add=codex");
      await click(q(drawer(), "su-signin-start"));
      await settle(() => !!q(drawer(), "su-signin-stopped"));
      const stopped = q(drawer(), "su-signin-stopped")!;
      expect(stopped.getAttribute("data-state")).toBe(state);
      expect(stopped.querySelector(".su-pill")!.textContent).toBe(S[state].headline);
      expect(stopped.querySelector(".su-pill")!.className).toContain("su-tone-red");
      expect(q(stopped, "su-signin-fix")!.textContent).toBe(S[state].fix);
      expect(q(drawer(), "su-signin-start")!.textContent).toBe("Try again");
      expect(connect().disabled).toBe(true);
      // Try again starts a fresh sign-in.
      await click(q(drawer(), "su-signin-start"));
      expect(api.calls.filter((c) => c.op === "sign_in_start")).toHaveLength(2);
    });
  }

  it("a refused start (409 {state, headline, fix}) shows the headline and the fix", async () => {
    const api = fakeApi(view());
    script(api, { start: S.refused_not_installed });
    mounted = await mountBoard(view(), "?area=setup&add=grok");
    await click(q(drawer(), "su-signin-start"));
    const stopped = q(drawer(), "su-signin-stopped")!;
    expect(stopped.querySelector(".su-pill")!.textContent).toBe("Grok is not on the BOSS server");
    expect(q(stopped, "su-signin-fix")!.textContent).toBe("Install the grok CLI on the server, then sign in.");
    expect(q(drawer(), "su-signin-start")!.textContent).toBe("Try again");
    expect(connect().disabled).toBe(true);
  });
});

describe("cancel", () => {
  it("Cancel stops the login on the server and says so", async () => {
    const api = fakeApi(view());
    script(api, { start: S.waiting, polls: [S.waiting] });
    signInTiming.pollMs = 60_000;
    mounted = await mountBoard(view(), "?area=setup&add=codex");
    await click(q(drawer(), "su-signin-start"));
    await click(q(drawer(), "su-signin-cancel"));
    const sent = api.calls.find((c) => c.op === "sign_in_cancel")!;
    expect(sent.body).toMatchObject({ sign_in_id: "si_codex_1", project_id: "p" });
    const stopped = q(drawer(), "su-signin-stopped")!;
    expect(stopped.getAttribute("data-state")).toBe("cancelled");
    expect(stopped.querySelector(".su-pill")!.className).toContain("su-tone-grey");
    expect(q(drawer(), "su-signin-start")!.textContent).toBe("Try again");
  });

  it("closing the drawer while it waits cancels the login", async () => {
    const api = fakeApi(view());
    script(api, { start: S.waiting, polls: [S.waiting] });
    signInTiming.pollMs = 60_000;
    mounted = await mountBoard(view(), "?area=setup&add=codex");
    await click(q(drawer(), "su-signin-start"));
    expect(api.calls.some((c) => c.op === "sign_in_cancel")).toBe(false);
    await press(document.body, "Escape");
    await flush();
    expect(drawer()).toBeNull();
    const sent = api.calls.find((c) => c.op === "sign_in_cancel")!;
    expect(sent.body).toMatchObject({ sign_in_id: "si_codex_1" });
  });
});

describe("already signed in on the server", () => {
  it("Connect is ready at once, starts no sign-in, and sends no credential", async () => {
    const v = view((x) => {
      x.detected = x.detected.map((d) => d.provider === "codex" ? { ...d, found: "signed_in" } : d);
    });
    const api = fakeApi(v);
    mounted = await mountBoard(v, "?area=setup&add=codex");
    expect(q(drawer(), "su-signed-in-on-server")).not.toBeNull();
    expect(q(drawer(), "su-signin-start")).toBeNull();
    expect(connect().disabled).toBe(false);
    await click(connect());
    expect(api.calls.some((c) => c.op === "sign_in_start")).toBe(false);
    expect(api.calls.find((c) => c.op === "models")!.body)
      .toEqual({ op: "models", project_id: "p", provider: "codex", kind: "subscription" });
  });

  it("a proof that fails keeps the drawer open on its pill, with Sign in right there", async () => {
    const v = view((x) => {
      x.detected = x.detected.map((d) => d.provider === "codex" ? { ...d, found: "signed_in" } : d);
    });
    const api = fakeApi(v);
    const after = view((x) => {
      x.models = [...x.models, { source_id: "openai_codex_chatgpt", provider: "codex", label: "Codex",
        kind: "subscription", model: null, used_for: [], pill: PILLS.loginExpired,
        checked_at: null, source: "connected", retry_at: null }];
    });
    api.answer = (call) => call.op === "models" ? { status: 200, body: after } : null;
    mounted = await mountBoard(v, "?area=setup&add=codex");
    await click(connect());
    const result = q(drawer(), "su-connect-result")!;
    expect(result.textContent).toBe("Login expired");
    expect(result.className).toContain("su-tone-red");
    expect(q(drawer(), "su-signin-start")!.textContent).toBe("Sign in");
  });
});

describe("a row whose login failed signs in, never Replace", () => {
  function expiredRow(): SetupView {
    return view((x) => {
      x.models = [{ source_id: "openai_codex_chatgpt", provider: "codex", label: "Codex",
        kind: "subscription", model: null, used_for: ["Build"], pill: PILLS.loginExpired,
        checked_at: null, source: "connected", retry_at: null }, ...x.models.slice(1)];
    });
  }

  it("the pill action sign_in shows Sign in, and opens the sign-in flow for that provider", async () => {
    const api = fakeApi(expiredRow());
    script(api, { start: S.waiting, polls: [S.waiting] });
    signInTiming.pollMs = 60_000;
    mounted = await mountBoard(expiredRow());
    const row = q(document.body, "su-model-openai_codex_chatgpt")!;
    expect(row.querySelector(".su-pill")!.textContent).toBe("Login expired");
    expect([...row.querySelectorAll("button")].some((b) => b.textContent?.trim() === "Replace")).toBe(false);
    await click(q(row, "su-model-sign-in"));
    expect(drawer()).not.toBeNull();
    // One press: the drawer opens on Codex and the sign-in has started.
    const start = api.calls.find((c) => c.op === "sign_in_start")!;
    // A refused login is replaced, not trusted: BOSS signs the stale session out first.
    expect(start.body).toMatchObject({ provider: "codex", fresh: true });
    expect(q(drawer(), "su-signin-open")!.getAttribute("href")).toBe("https://auth.openai.com/codex/device");
    expect(q(row, "su-model-pill")!.textContent).toBe("Signing in…");
    expect(drawer().querySelectorAll('input[type="password"], input[type="file"]').length).toBe(0);
  });

  it("the row menu says Sign in for a subscription, Replace only for a key", async () => {
    fakeApi(expiredRow());
    mounted = await mountBoard(expiredRow());
    const codex = q(document.body, "su-model-openai_codex_chatgpt")!;
    await click(q(codex, "su-model-menu"));
    expect(q(codex, "su-menu-sign-in")).not.toBeNull();
    expect(q(codex, "su-menu-replace")).toBeNull();
    const key = q(document.body, "su-model-setup-openrouter")!;
    await click(q(key, "su-model-menu"));
    expect(q(key, "su-menu-replace")).not.toBeNull();
    expect(q(key, "su-menu-sign-in")).toBeNull();
  });

  it("Connect after signing in from the row does not replace a source", async () => {
    const api = fakeApi(expiredRow());
    script(api, { start: S.waiting, polls: [S.signed_in] });
    mounted = await mountBoard(expiredRow());
    await click(q(q(document.body, "su-model-openai_codex_chatgpt")!, "su-model-sign-in"));
    await settle(() => !!q(drawer(), "su-signin-done"));
    await click(connect());
    expect(api.calls.find((c) => c.op === "models")!.body)
      .toEqual({ op: "models", project_id: "p", provider: "codex", kind: "subscription" });
  });
});

describe("the Sources badge is the Models row's pill", () => {
  function sourcesApi(v: SetupView) {
    const api = fakeApi(v);
    api.answer = (call) => call.url.startsWith("/v1/entitlements/sources?") ? { status: 200, body: SOURCES }
      : call.url.startsWith("/v1/entitlement-connection-sessions?") ? { status: 200, body: { items: [] } } : null;
    return api;
  }
  const badge = (id: string) => document.querySelector(
    `[data-testid="setup-cap-source"][data-source="${id}"] [data-testid="su-model-pill"]`) as HTMLElement;

  it("a login_expired pill reads Login expired in red, never Active · Available", async () => {
    sourcesApi(board());
    mounted = await mountBoard(board());
    await settle(() => !!badge("openai_codex_chatgpt"));
    const codex = badge("openai_codex_chatgpt");
    expect(codex.textContent).toBe("Login expired");
    expect(codex.className).toContain("su-tone-red");
    expect(codex.getAttribute("data-state")).toBe("login_expired");
    const row = codex.closest('[data-testid="setup-cap-source"]')!;
    expect(row.textContent).not.toContain("Active");
    expect(row.textContent).not.toContain("Available");
    // A proven pill shows as itself; no pill falls back to the connection record.
    expect(badge("anthropic_api").textContent).toBe("Working");
    expect(badge("xai_grok_build").textContent).toBe("Active");
    expect(badge("xai_grok_build").className).toContain("su-tone-green");
  });

  it("no session here shows no Sessions line at all (it said nothing on every install)", async () => {
    sourcesApi(board());
    mounted = await mountBoard(board());
    await settle(() => !!badge("openai_codex_chatgpt"));
    expect(q(document.body, "setup-entitlement-session")).toBeNull();
  });
});


describe("the live stale Codex login is fixable from the console alone (2026-10-25)", () => {
  function staleView(): SetupView {
    // `codex login status` says signed in; the last proof says Login expired.
    return view((x) => {
      x.detected = x.detected.map((d) => d.provider === "codex" ? { ...d, found: "signed_in" } : d);
      x.models = [{ source_id: "openai_codex_chatgpt", provider: "codex", label: "Codex",
        kind: "subscription", model: null, used_for: ["Build"], pill: PILLS.loginExpired,
        checked_at: null, source: "connected", retry_at: null }, ...x.models.slice(1)];
    });
  }
  function sourcesApi(v: SetupView) {
    const api = fakeApi(v);
    const before = api.answer;
    api.answer = (call) => call.url.startsWith("/v1/entitlements/sources?") ? { status: 200, body: SOURCES }
      : call.url.startsWith("/v1/entitlement-connection-sessions?") ? { status: 200, body: { items: [] } }
      : before(call);
    return api;
  }
  const source = (id: string) => document.querySelector(
    `[data-testid="setup-cap-source"][data-source="${id}"]`) as HTMLElement;

  it("the drawer says the last check was refused beside Connect; Sign in again replaces the login", async () => {
    const api = fakeApi(staleView());
    script(api, { start: S.waiting, polls: [S.waiting] });
    signInTiming.pollMs = 60_000;
    mounted = await mountBoard(staleView(), "?area=setup&add=codex");
    const status = q(drawer(), "su-signed-in-on-server")!;
    expect(status.textContent).toBe("Check refused");
    expect(status.getAttribute("data-state")).toBe("refused");
    // Left of Connect, on Connect's own line.
    expect(status.parentElement).toBe(connect().parentElement);
    expect(connect().disabled).toBe(false);
    expect(api.calls.some((c) => c.op === "sign_in_start")).toBe(false);
    await click(q(drawer(), "su-signin-again"));
    await click(q(drawer(), "su-signin-start"));
    expect(api.calls.find((c) => c.op === "sign_in_start")!.body)
      .toMatchObject({ provider: "codex", fresh: true });
  });

  it("a Login expired row whose CLI says signed in offers Connect — it never signs the login out", async () => {
    const api = sourcesApi(staleView());
    script(api, { start: S.waiting, polls: [S.waiting] });
    signInTiming.pollMs = 60_000;
    mounted = await mountBoard(staleView());
    await settle(() => !!source("openai_codex_chatgpt"));
    const codex = source("openai_codex_chatgpt");
    expect(codex.querySelector('[data-testid="su-model-pill"]')!.textContent).toBe("Login expired");
    expect(q(codex, "su-model-sign-in")).toBeNull();
    // Not the official-session form (Complete session would mark it connected with no proof).
    expect(q(codex, "setup-cap-start-open")).toBeNull();
    await click(q(codex, "su-model-connect-row"));
    const sheet = drawer();
    expect(sheet.getAttribute("aria-label")).toBe("Connect");
    expect(api.calls.some((c) => c.op === "sign_in_start")).toBe(false);
    await click(connect());
    expect(api.calls.find((c) => c.op === "models")!.body)
      .toEqual({ op: "models", project_id: "p", provider: "codex", kind: "subscription" });
  });

  it("a Login expired row whose CLI is signed out offers Sign in, which replaces the dead session", async () => {
    const v = () => { const x = staleView(); x.detected = x.detected.map((d) => d.provider === "codex" ? { ...d, found: "needs_sign_in" } : d); return x; };
    const api = sourcesApi(v());
    script(api, { start: S.waiting, polls: [S.waiting] });
    signInTiming.pollMs = 60_000;
    mounted = await mountBoard(v());
    await settle(() => !!source("openai_codex_chatgpt"));
    await click(q(source("openai_codex_chatgpt"), "su-model-sign-in"));
    const sheet = drawer();
    expect(api.calls.find((c) => c.op === "sign_in_start")!.body)
      .toMatchObject({ provider: "codex", fresh: true });
    expect(q(sheet, "su-signin-open")!.getAttribute("href")).toBe("https://auth.openai.com/codex/device");
    expect(sheet.querySelectorAll('input[type="password"], input[type="file"]').length).toBe(0);
  });

  it("a healthy connected subscription offers no Sign in", async () => {
    sourcesApi(staleView());
    mounted = await mountBoard(staleView());
    await settle(() => !!source("xai_grok_build"));
    expect(q(source("xai_grok_build"), "setup-cap-sign-in")).toBeNull();
  });
});
