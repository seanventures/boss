// @vitest-environment happy-dom
/**
 * INSIDER WORDS STAY OFF A STRANGER'S CONSOLE (stranger test 2026-11-17):
 *   - "Self-fixes" and "Landed slices" show only on an install that has fixed BOSS itself (any
 *     self-fix on record); a fresh install hides both; an unknown answer hides nothing
 *   - the model picker never lists a source this image cannot connect (Cursor CLI / API, the
 *     Gemini CLI), and the docs say the same; "TypeSafe Jev" says what it is for
 */
import fs from "node:fs";
import path from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
vi.mock("../lib/use-agent-identities", () => ({ useLoadIdentities: () => undefined, useIdentityVersion: () => 0 }));
vi.mock("../lib/agents-live", () => ({ useAttention: () => null, useLivePresence: () => new Map() }));
vi.mock("../app/agent-ui", () => ({ AgentChat: () => null }));
vi.mock("../app/project-strip", () => ({ ProjectStrip: () => null }));
vi.mock("../app/agents/converse-host", () => ({ ConverseHost: () => null }));
vi.mock("../components/identity/consent-sheet", () => ({ ConsentHost: () => null }));
vi.mock("../components/identity/identity-provider", () => {
  const allowed = { allowed: true, reason: "", needsStepUp: false };
  const identity = { me: null, check: () => allowed, stub: false, mirrored: true };
  return {
    IdentityProvider: ({ children }: { children: React.ReactNode }) => children,
    useIdentity: () => identity,
    useCan: () => allowed,
  };
});

import ConsoleShell from "../app/ConsoleShell";
import { NOT_OFFERED, offeredInPicker, sourceName } from "../app/setup/capacity-row";
import { forgetInsider, insiderFrom, insiderShown } from "../lib/insider-areas";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the insider rule", () => {
  it("shows with self-fixes on record, hides when the list is empty or not readable, guesses nothing", () => {
    expect(insiderFrom({ ok: true, data: { self_fixes: [], count: 3 }, etag: null })).toBe(true);
    expect(insiderFrom({ ok: true, data: { self_fixes: [], count: 0 }, etag: null })).toBe(false);
    expect(insiderFrom({ ok: false, status: 403, error: "no" })).toBe(false);
    expect(insiderFrom({ ok: false, status: 0, error: "network" })).toBeNull();
    expect(insiderFrom({ ok: false, status: 502, error: "down" })).toBeNull();
    expect(insiderFrom({ ok: true, data: {} as never, etag: null })).toBeNull();
    expect(insiderFrom({ ok: true, data: { exists: true }, etag: null })).toBe(true);
    expect(insiderFrom({ ok: true, data: { exists: false }, etag: null })).toBe(false);
    expect(insiderShown(null)).toBe(true);
    expect(insiderShown(false)).toBe(false);
  });
});

let host: HTMLDivElement;
let root: Root;
beforeEach(() => { host = document.createElement("div"); document.body.append(host); root = createRoot(host); window.localStorage.clear(); forgetInsider(); });
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.unstubAllGlobals(); });

/** `selfFixes` is the open project's own list; `install` what the whole install has. */
async function shell(selfFixes: number, install = selfFixes) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const u = String(url);
    const body = u.includes("/api/self-fix") && u.includes("view=exists") ? { exists: install > 0 }
      : u.includes("/api/self-fix") ? { self_fixes: [], count: selfFixes } : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  window.history.replaceState(null, "", "/?area=overview");
  await act(async () => root.render(
    <ConsoleShell initialArea="overview" loadedAreas={["overview"]}
      scope={{ project: "Default project", actor: "Stranger", projectId: "default" }}
      org={{ id: "org_1", name: "Org" }} projects={{ items: [{ id: "default", name: "Default project" }], homeId: "default" }}
      status={{ apiReachable: true, budgetState: "green", deadletters: 0, pendingDecisions: 0 }}>
      <section data-area="overview">overview</section>
    </ConsoleShell>));
  await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
}

describe("the rail", () => {
  it("a fresh install shows neither Self-fixes nor Landed slices", async () => {
    await shell(0);
    expect(host.querySelector('[data-area-nav="self_fixes"]')).toBeNull();
    expect(host.querySelector('[data-testid="reconcile-mounts"]')).toBeNull();
  });

  it("an install that has fixed BOSS itself shows both", async () => {
    await shell(2);
    expect(host.querySelector('[data-area-nav="self_fixes"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="reconcile-mounts"]')).not.toBeNull();
  });
});

describe("the operator never loses them (review of #200)", () => {
  it("a project with no self-fixes still shows both when another project of the install has some", async () => {
    await shell(0, 27);
    expect(host.querySelector('[data-area-nav="self_fixes"]')).not.toBeNull();
    expect(host.querySelector('[data-testid="reconcile-mounts"]')).not.toBeNull();
  });

  it("a true answer is kept for the session: a later probe that fails or says no does not hide them", async () => {
    await shell(0, 3);
    await act(async () => root.unmount());
    root = createRoot(host);
    await shell(0, 0);
    expect(host.querySelector('[data-area-nav="self_fixes"]')).not.toBeNull();
  });
});

describe("the model picker and the docs agree", () => {
  it("Cursor CLI, Cursor API and the Gemini CLI are not offered; Gemini stays as an API key", () => {
    for (const id of ["cursor_cli", "cursor_api", "google_gemini_cli"]) expect(offeredInPicker(id), id).toBe(false);
    for (const id of ["anthropic_claude_code", "openai_codex_chatgpt", "xai_grok_build", "google_api"]) {
      expect(offeredInPicker(id), id).toBe(true);
    }
  });

  it("the decision model (TypeSafe Jev) is not offered under More sources until a key is configured", () => {
    expect(offeredInPicker("typesafe_jev")).toBe(false);
  });

  it("TypeSafe Jev says what it is for", () => {
    expect(sourceName("typesafe_jev")).toBe("Decision model (TypeSafe Jev)");
  });

  it("MODEL_CONNECTIONS.md names every source the picker leaves out as not offered", () => {
    const doc = fs.readFileSync(path.resolve(__dirname, "../../../docs/community/MODEL_CONNECTIONS.md"), "utf8");
    expect(doc).toMatch(/Cursor \(its CLI or its API\) and the Gemini CLI are not offered/);
    expect(NOT_OFFERED.size).toBe(3);
    for (const id of NOT_OFFERED) expect(doc).toContain(sourceName(id).replace(/ (CLI|API)$/, ""));
  });
});
