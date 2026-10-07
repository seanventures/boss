// @vitest-environment happy-dom
/**
 * Access roles and the Identity overview, the parts FX-C changed:
 *   - the impact preview names every group with its own count, in plain grammar
 *     ("2 direct, 1 through Builders and 3 through Release"), never "· 1 through the group";
 *   - a custom role can be deleted; one with holders moves them to another role first (one
 *     POST …/reassign {to_role_id, delete:true}); an empty one is a plain DELETE;
 *   - where a role binds is editable, and the save sends bindable_scopes only when it changed;
 *   - no custom roles: an empty state that says how to make one;
 *   - the overview: no security events is an explained empty state, an event reads in words
 *     (not access_role.updated), a failed read says so instead of looking locked, the "only person
 *     here" invite replaces the people table, and no line is drawn that is not a real link;
 *   - nothing raw on screen, and the copy budget holds.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { RoleEditor, impactSplit } from "../app/identity/roles/[id]/role-editor";
import { RoleList } from "../app/identity/roles/role-list";
import { Overview } from "../app/identity/overview";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import { resetStub, stubHandle } from "../lib/identity/stub/server";
import type { AccessRole } from "../lib/identity/types";
import { eventWords } from "../lib/identity/words";
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

function wire(s: { cookies: Record<string, string> }) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ method, path: url.pathname, body });
    const out = stubHandle({ method, path: url.pathname, query: url.searchParams, body, cookies: s.cookies, headers: new Headers(init?.headers) });
    return out.status === 204 ? new Response(null, { status: 204 })
      : new Response(JSON.stringify(out.body), { status: out.status, headers: { "content-type": "application/json" } });
  }));
}

beforeEach(() => { resetStub(NOW); calls = []; });
afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  document.body.innerHTML = "";
  root = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function settle() {
  for (let i = 0; i < 8; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
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
const q = (sel: string) => document.querySelector(sel) as HTMLElement | null;
async function click(el: Element | null) {
  expect(el, "element to click").not.toBeNull();
  await act(async () => { (el as HTMLElement).click(); });
  await settle();
}
async function choose(el: Element | null, value: string) {
  const s = el as HTMLSelectElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(s, value);
    s.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await settle();
}
function expectClean(el: Element) {
  expect(rawLeaks(el), "raw keys, codes or ids on screen").toEqual([]);
  const a = identityCopyAudit(el);
  expect(a.labels, "labels over 6 words").toEqual([]);
  expect(a.prose, "prose over 24 words").toEqual([]);
  expect(a.tips, "tooltips over 14 words").toEqual([]);
}

const roles = () => F.roles(NOW);
const captain = (): AccessRole => roles().find((r) => r.key === "release_captain")!;

describe("the impact preview's words", () => {
  it("names every group with its own count", () => {
    expect(impactSplit(2, [])).toBe("2 direct");
    expect(impactSplit(2, [{ group: "Release", count: 1 }])).toBe("2 direct and 1 through Release");
    expect(impactSplit(2, [{ group: "Builders", count: 1 }, { group: "Release", count: 3 }])).toBe("2 direct, 1 through Builders and 3 through Release");
    expect(impactSplit(0, [{ group: "Builders", count: 1 }, { group: "Release", count: 3 }])).not.toContain("through the group");
  });
});

describe("deleting a custom role", () => {
  it("one with holders moves them to another role first, in one call", async () => {
    const owner = as("param@kaappu.org");
    const assign = vi.fn();
    vi.spyOn(window.location, "assign").mockImplementation(assign);
    const el = await mount(owner, <RoleEditor role={captain()} versions={[]} roles={roles()} />);
    expectClean(el);
    await click(q("[data-testid='delete-role-open']"));
    const sheet = q("[data-testid='delete-role']")!;
    expect(sheet.textContent).toContain("Move them to another role first");
    expect(q("[data-testid='delete-role-confirm']")?.hasAttribute("disabled")).toBe(true);
    await choose(q("[data-testid='reassign-to']"), roles().find((r) => r.key === "operator")!.id);
    expectClean(sheet);
    await click(q("[data-testid='delete-role-confirm']"));
    const call = calls.find((c) => c.path.endsWith("/reassign"))!;
    expect(call.body).toEqual({ to_role_id: roles().find((r) => r.key === "operator")!.id, delete: true });
    expect(assign).toHaveBeenCalledWith("/identity/roles");
  });

  it("an unheld one is a plain DELETE; a 409 role_in_use switches to moving its holders", async () => {
    const owner = as("param@kaappu.org");
    vi.spyOn(window.location, "assign").mockImplementation(() => undefined);
    // The console thought nobody held it; the API knows better.
    await mount(owner, <RoleEditor role={{ ...captain(), holders: 0 }} versions={[]} roles={roles()} />);
    await click(q("[data-testid='delete-role-open']"));
    expect(q("[data-testid='reassign-to']")).toBeNull();
    await click(q("[data-testid='delete-role-confirm']"));
    expect(calls.some((c) => c.method === "DELETE" && c.path === "/v1/identity/access-roles/arl_release_captain")).toBe(true);
    expect(q("[data-testid='reassign-to']")).not.toBeNull();
    expect(q("[data-testid='delete-role']")?.textContent).not.toMatch(/role_in_use/);
  });

  it("a Viewer sees Delete greyed with the permission's name; built-in roles have no Delete", async () => {
    const viewer = as("diego@acme.dev");
    await mount(viewer, <RoleEditor role={captain()} versions={[]} roles={roles()} />);
    expect(q("[data-testid='delete-role-open']")?.getAttribute("title")).toBe("Needs “Manage access roles” · ask an Admin");
    act(() => root?.unmount());
    host?.remove();
    const owner = as("param@kaappu.org");
    await mount(owner, <RoleEditor role={roles().find((r) => r.key === "viewer")!} versions={[]} roles={roles()} />);
    expect(q("[data-testid='delete-role-open']")).toBeNull();
  });
});

describe("where a role binds", () => {
  it("is editable, keeps one on, and the save sends it only when changed", async () => {
    const owner = as("param@kaappu.org");
    await mount(owner, <RoleEditor role={captain()} versions={[]} roles={roles()} />);
    await click(q("[data-testid='scope-project']"));
    expect(q("[data-testid='scope-project']")?.getAttribute("aria-pressed")).toBe("false");
    expect(q("[data-testid='scope-org']")?.getAttribute("aria-disabled")).toBe("true");
    await click(q("[data-testid='save']"));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect((patch.body as { bindable_scopes?: string[] }).bindable_scopes).toEqual(["org"]);
  });
});

describe("the roles list", () => {
  it("with no custom roles: says how to make one", async () => {
    const owner = as("param@kaappu.org");
    const el = await mount(owner, <RoleList roles={roles().filter((r) => r.builtin)} />);
    expect(q("[data-testid='roles-empty']")?.textContent).toContain("Clone a built-in role");
    expectClean(el);
  });
});

describe("Identity › Overview", () => {
  const base = () => {
    const people = F.people(NOW);
    return { posture: null, people, roles: roles(), groups: F.groups(NOW, people), serviceAccounts: F.serviceAccounts(NOW), agents: F.passports(NOW) };
  };

  it("events read in words; none is an explained empty state", async () => {
    const owner = as("param@kaappu.org");
    const events = F.auditEvents(NOW).slice(0, 4);
    const el = await mount(owner, <Overview {...base()} events={events} />);
    expect(el.textContent).toContain(eventWords(events[0].event_type));
    expect(el.textContent).not.toContain(events[0].event_type);
    expectClean(el);
    act(() => root?.unmount());
    host?.remove();
    const el2 = await mount(owner, <Overview {...base()} events={[]} />);
    expect(q("[data-testid='events-empty']")?.textContent).toContain("No security events yet");
    expectClean(el2);
  });

  it("a failed read says so, not 'needs a permission'", async () => {
    const owner = as("param@kaappu.org");
    const el = await mount(owner, <Overview posture={null} people={null} roles={null} groups={null} serviceAccounts={null} agents={null} events={null}
      failures={{ people: { status: 500, code: "error" }, events: { status: 503, code: "identity_unavailable" }, posture: { status: 500, code: "error" } }} />);
    expect(el.textContent).toContain("Couldn't load people");
    expect(el.textContent).toContain("Identity is unreachable");
    expect(el.textContent).toContain("Couldn't load the security posture");
    expect(el.querySelector("[data-testid='locked-people']")).toBeNull();
    expectClean(el);
  });

  it("alone: the invite replaces the people table", async () => {
    const owner = as("param@kaappu.org");
    const me = F.people(NOW).filter((p) => p.id === "usr_param");
    const el = await mount(owner, <Overview posture={null} people={me} roles={roles()} groups={[]} serviceAccounts={[]} agents={[]} events={[]} />);
    expect(q("[data-testid='alone']")?.textContent).toContain("You're the only person here");
    expect(el.querySelectorAll("table").length).toBe(0);
    expect(el.textContent).not.toContain("from your IdP");
    expectClean(el);
  });

  it("draws a line only for a person who is acting, from the role they hold", async () => {
    const owner = as("param@kaappu.org");
    const posture = owner.get<import("../lib/identity/types").Posture>("/v1/identity/posture");
    const el = await mount(owner, <Overview {...base()} posture={posture} events={[]} />);
    const lines = el.querySelectorAll(".idn-wire path").length;
    const people = F.people(NOW);
    const actingKnown = posture.acting_now.filter((a) => people.some((p) => p.id === a.user.id && p.status === "active"
      && p.bindings.some((b) => !b.via_group && ["owner", "admin", "operator", "viewer", "auditor"].includes(b.role_key)))).length;
    expect(lines).toBe(actingKnown);
    expect(ROLE_PERMISSIONS.owner.length).toBeGreaterThan(0);
  });
});
