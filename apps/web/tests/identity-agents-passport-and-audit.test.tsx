// @vitest-environment happy-dom
/**
 * Agents, Passports, a delegation and the audit log (FX-C W4), against the identity stub:
 *   - an agent with no sponsor offers Assign, which PATCHes the sponsor and shows them;
 *   - the "Ceiling" is called the access limit, with one line saying what it is;
 *   - empty delegations, tokens and egress say what they are and what to do; egress reads
 *     "allowed"/"refused", the HTTP code only in a tooltip;
 *   - the audit log names events in words (the raw type only in a tooltip) and never prints a
 *     token id; a failed read is an ErrorState, never "no events";
 *   - no screen shows a permission key, a raw id, an error code or set notation; the copy budget holds.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { ErrorState } from "../components/identity/states";
import { DelegationView, egressWords } from "../app/identity/agents/delegations/[id]/delegation";
import { detailFromGrant } from "../app/identity/agents/delegations/[id]/detail";
import { AgentsScreen } from "../app/identity/agents/agents-screen";
import { PassportScreen } from "../app/identity/agents/[id]/passport";
import { AuditScreen } from "../app/identity/audit/audit-screen";
import { EMPTY_FILTERS } from "../app/identity/audit/audit-query";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { plainText } from "../lib/identity/plain";
import { resetStub, stubHandle } from "../lib/identity/stub/server";
import type { AuditPage, Delegation, DelegationDetail, Passport } from "../lib/identity/types";
import { identityCopyAudit } from "./_helpers/identity-copy";
import { rawLeaks } from "./_helpers/identity-raw";
import { req, signIn } from "./_helpers/identity-stub-session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.now();
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; path: string; body: unknown }[] = [];

function as(email: string) {
  const s = signIn(email);
  const get = <T,>(path: string) => stubHandle(req("GET", path, { cookies: s.cookies })).body as T;
  return { s, me: get<MeResponse>("/v1/identity/me"), org: get<MePermissionsResponse>("/v1/identity/me/permissions"), get };
}

function wire(s: { cookies: Record<string, string>; csrf: string }) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ method, path: url.pathname, body });
    const preview = /^(\/v1\/identity\/delegations\/[^/]+)\/cascade-preview$/.exec(url.pathname);
    const out = preview
      ? stubHandle({ method: "POST", path: `${preview[1]}/revoke`, query: url.searchParams, body: { dry_run: true }, cookies: s.cookies, headers: new Headers({ "x-boss-csrf": s.csrf }) })
      : stubHandle({ method, path: url.pathname, query: url.searchParams, body, cookies: s.cookies, headers: new Headers({ ...Object.fromEntries(new Headers(init?.headers)), "x-boss-csrf": s.csrf }) });
    return new Response(JSON.stringify(out.body), { status: out.status, headers: { "content-type": "application/json" } });
  }));
}

beforeEach(() => { resetStub(NOW); calls = []; });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.querySelectorAll(".idn-sheet-wrap").forEach((n) => n.remove());
  root = null;
  vi.unstubAllGlobals();
});

async function settle(n = 6) {
  for (let i = 0; i < n; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
async function mount(who: ReturnType<typeof as>, node: React.ReactElement) {
  wire(who.s);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={who.me} org={who.org}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>));
  await settle();
  return host;
}
async function click(el: Element | null) { expect(el, "element to click").not.toBeNull(); await act(async () => { (el as HTMLElement).click(); }); await settle(); }
function choose(el: Element | null, value: string) {
  const sel = el as HTMLSelectElement;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(sel, value);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
function noLeaks() {
  for (const el of [host!, ...document.querySelectorAll(".idn-sheet-wrap")]) {
    expect(rawLeaks(el), "raw ids, codes or jargon on screen").toEqual([]);
    const a = identityCopyAudit(el);
    expect([...a.labels, ...a.prose, ...a.tips], "copy budget").toEqual([]);
  }
}
const PEOPLE = [{ id: "usr_sam", name: "Sam Okafor", hue: 6 }, { id: "usr_priya", name: "Priya Raman", hue: 5 }];

describe("Passports", () => {
  it("an agent with no sponsor gets one assigned, by name", async () => {
    const owner = as("param@example.com");
    const agents = owner.get<{ agents: Passport[] }>("/v1/identity/agents").agents;
    const orphan = agents.find((a) => !a.sponsor)!;
    expect(orphan).toBeDefined();
    const el = await mount(owner, <PassportScreen initial={orphan} grants={[]} people={PEOPLE} />);
    expect(el.textContent).toContain("No sponsor");
    await click(el.querySelector("[data-testid='assign-sponsor']"));
    choose(document.querySelector("[data-testid='sponsor-pick-add']"), "usr_priya");
    await settle();
    noLeaks();
    await click(document.querySelector("[data-testid='sponsor-save']"));
    await settle(8);
    expect(calls.find((c) => c.method === "PATCH")).toMatchObject({ path: `/v1/identity/agents/${orphan.id}`, body: { sponsor: "usr_priya" } });
    expect(el.textContent).toContain("Priya Raman");
    expect(el.textContent).not.toContain("No sponsor");
  });

  it("a Viewer sees Assign greyed with the permission's name", async () => {
    const viewer = as("diego@acme.dev");
    const agents = as("param@example.com").get<{ agents: Passport[] }>("/v1/identity/agents").agents;
    const el = await mount(viewer, <PassportScreen initial={agents.find((a) => !a.sponsor)!} grants={[]} people={PEOPLE} />);
    const b = el.querySelector("[data-testid='assign-sponsor']")!;
    expect(b.getAttribute("aria-disabled")).toBe("true");
    expect(b.getAttribute("title")).toMatch(/^Needs “.+” · ask an (Admin|Owner)$/);
  });

  it("calls the ceiling the access limit, and every Passport reads without raw ids or set notation", async () => {
    const owner = as("param@example.com");
    const agents = owner.get<{ agents: Passport[] }>("/v1/identity/agents").agents;
    for (const a of agents.slice(0, 6)) {
      const el = await mount(owner, <PassportScreen initial={a} grants={null} people={PEOPLE} />);
      expect(el.querySelector("#idn-ceiling-h")?.textContent).toBe("Access limit");
      noLeaks();
      act(() => root?.unmount());
      host?.remove();
    }
  });

  it("the agents list and its delegations read in words; an empty one says what to do", async () => {
    const owner = as("param@example.com");
    const agents = owner.get<{ agents: Passport[] }>("/v1/identity/agents").agents;
    const delegations = owner.get<{ delegations: Delegation[] }>("/v1/identity/delegations").delegations;
    await mount(owner, <AgentsScreen agents={agents} delegations={delegations} />);
    noLeaks();
    act(() => root?.unmount());
    host?.remove();
    const el = await mount(owner, <AgentsScreen agents={[]} delegations={[]} />);
    expect(el.querySelector("[data-testid='agents-empty']")).not.toBeNull();
    expect(el.querySelector("[data-testid='delegations-empty'] a")?.getAttribute("href")).toBe("/account/agents?new=1");
    noLeaks();
  });

  it("a failed delegations read is said, never drawn as none", async () => {
    const owner = as("param@example.com");
    const el = await mount(owner, <AgentsScreen agents={[]} delegations={null} delegationsFailure={{ status: 503, code: "identity_unavailable" }} />);
    expect(el.querySelector("[data-testid='error-state']")?.textContent).toContain("Identity is unreachable");
    expect(el.querySelector("[data-testid='delegations-empty']")).toBeNull();
    noLeaks();
  });
});

describe("a delegation", () => {
  it("reads in plain words: chain rule, hops, tokens and egress", async () => {
    const owner = as("param@example.com");
    const d = owner.get<DelegationDetail>("/v1/identity/delegations/dlg_01JB7Q");
    const el = await mount(owner, <DelegationView initial={d} />);
    expect(el.querySelector(".idn-chain-rule")?.textContent).toContain("Agents get only what");
    noLeaks();
  });

  it("with no chain, tokens or egress yet, says so", async () => {
    const owner = as("param@example.com");
    const grant = owner.get<{ delegations: Delegation[] }>("/v1/identity/delegations").delegations[0];
    const el = await mount(owner, <DelegationView initial={detailFromGrant(grant)} />);
    expect(el.querySelector("[data-testid='tokens-empty']")).not.toBeNull();
    expect(el.querySelector("[data-testid='egress-empty']")).not.toBeNull();
    noLeaks();
  });

  it("egress answers are words; plain text drops ids and set notation", () => {
    expect([200, 201, 403, 401, 404, 429, 500].map(egressWords)).toEqual(["allowed", "allowed", "refused", "refused", "not found", "rate limited", "failed"]);
    expect(plainText("∩ ceiling(Forge) · rt_7P · DPoP")).toBe("within Forge's limit · key-bound token");
    expect(plainText("+code_task:create −github:merge")).toBe("+Create builds −Merge pull requests".replace("Create builds", plainText("code_task:create")).replace("Merge pull requests", plainText("github:merge")));
    expect(plainText("key_7f3k2q · 140.82.112.4 · GitHub")).toBe("140.82.112.4 · GitHub");
  });
});

describe("the audit log", () => {
  it("names events in words, keeps the raw type in a tooltip, and never prints a token id", async () => {
    const owner = as("param@example.com");
    const page = owner.get<AuditPage>("/v1/identity/audit");
    const withToken = page.events.find((e) => e.token_jti) ?? page.events[0];
    const el = await mount(owner, <AuditScreen initial={page} initialFilters={EMPTY_FILTERS} initialEvent={withToken.id} />);
    const evc = el.querySelector(".idn-evc")!;
    expect(evc.getAttribute("title")).toBe(withToken.event_type);
    expect(evc.textContent).not.toContain(withToken.event_type);
    expect((el.querySelector("[data-testid='audit-search']") as HTMLInputElement).placeholder).not.toMatch(/jti/);
    noLeaks();
  });

  it("a failed read is an ErrorState with Try again, never an empty log", async () => {
    const owner = as("param@example.com");
    const el = await mount(owner, <AuditScreen initial={null} initialFilters={EMPTY_FILTERS} initialEvent={null} failure={{ status: 502, code: "error" }} />);
    const state = el.querySelector("[data-testid='error-state']")!;
    expect(state.textContent).toContain("Couldn't load the audit log");
    expect(state.textContent).toContain("Try again");
    expect(el.textContent).not.toContain("No security events");
    noLeaks();
  });

  it("ErrorState never shows the code it was given", async () => {
    const owner = as("param@example.com");
    for (const failure of [{ status: 503, code: "identity_unavailable" }, { status: 403, code: "forbidden", permission: "audit:read" }, { status: 500, code: "internal_error" }, { status: 0, code: "identity_unavailable" }]) {
      const el = await mount(owner, <ErrorState what="the audit log" failure={failure} />);
      expect(el.textContent).not.toContain(failure.code);
      noLeaks();
      act(() => root?.unmount());
      host?.remove();
    }
  });
});
