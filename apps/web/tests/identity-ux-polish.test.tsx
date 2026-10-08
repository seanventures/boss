// @vitest-environment happy-dom
/**
 * The operator's walk of BOSS Identity on boss-1 (2026-10-07), each defect fixed as a class:
 *
 *   - a private or container address (172.18.0.8 — the console's own container) is never shown
 *     as where someone was: sessions, a key's last use and the audit log show public ones only;
 *   - an id's qualifiers never leak once its prefix is stripped (":git:315ad114331e"), and a
 *     run's two identical lineage lines read once, with a count;
 *   - a Passport's access limit changes in an explicit mode with a preview, a locked ability
 *     says WHY in plain words, and its GitHub limit is editable — never above what the BOSS
 *     GitHub App can get — with a box saying how the agent reaches GitHub;
 *   - People offers Manage access on every row to whoever may assign roles.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { PassportScreen, lineageLines } from "../app/identity/agents/[id]/passport";
import { PeopleList } from "../app/identity/people/people-list";
import { MySessions } from "../app/account/sessions/sessions";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS, type Permission } from "../lib/identity/permissions.gen";
import { publicAddress, sourceWords } from "../lib/identity/address";
import { APP_PERMISSIONS, levelOffered, limitBody, limitChanges } from "../lib/identity/github-limit";
import { plainText } from "../lib/identity/plain";
import { neverDelegableWhy } from "../lib/identity/words";
import * as F from "../lib/identity/stub/fixtures";
import type { Passport } from "../lib/identity/types";
import { rawLeaks } from "./_helpers/identity-raw";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-07T20:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;
let calls: { method: string; url: string; body: unknown }[] = [];
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; vi.unstubAllGlobals(); });

const ME: MeResponse = {
  principal_id: "usr_param", kind: "user", display_name: "Param Gnanaguru", email: "param@acme.dev", org: F.ORG, status: "active", access_roles: ["owner"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
};
const perms = (list: readonly Permission[]): MePermissionsResponse => ({ epoch: 1, scope: "org", permissions: [...list], step_up_fresh_until: null, catalogue_fingerprint: "x" });

async function settle() { for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); }
async function mount(node: React.ReactElement, held: readonly Permission[] = ROLE_PERMISSIONS.owner, answer: (body: unknown) => unknown = () => ({})) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method: (init?.method ?? "GET").toUpperCase(), url: String(url), body });
    return new Response(JSON.stringify(answer(body)), { status: 200, headers: { "content-type": "application/json" } });
  }));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={ME} org={perms(held)}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>));
  await settle();
  return host;
}
const q = (sel: string) => host!.querySelector(sel) as HTMLElement | null;
async function click(el: Element | null) { expect(el).not.toBeNull(); await act(async () => { (el as HTMLElement).click(); }); await settle(); }

describe("a private or container address is never where someone was", () => {
  it("publicAddress keeps public addresses and drops private, container, tailnet and loopback ones", () => {
    for (const ip of ["172.18.0.8", "10.0.4.12", "192.168.1.2", "127.0.0.1", "100.64.0.20", "169.254.0.1", "::1", "fd00::1", "fe80::1", "::ffff:172.18.0.8", "internal", "", null]) {
      expect(publicAddress(ip), String(ip)).toBeNull();
    }
    for (const ip of ["203.0.113.9", "8.8.8.8", "172.32.0.1", "2001:4860:4860::8888"]) expect(publicAddress(ip)).toBe(ip);
    expect(publicAddress("::ffff:8.8.4.4")).toBe("8.8.4.4");
    expect(sourceWords("172.18.0.8")).toBe("private");
    expect(sourceWords("internal")).toBe("BOSS itself");
    expect(sourceWords("203.0.113.9")).toBe("203.0.113.9");
  });

  it("My account › Sessions shows no container address, and says how each signed in in one vocabulary", async () => {
    const at = new Date(NOW - 60_000).toISOString();
    const el = await mount(<MySessions sessions={[
      { id: "ses_1", current: true, device: "desktop", client: "Chrome on macOS", location: null, ip: "172.18.0.8", methods: ["pwd", "totp"], started_at: at, last_seen_at: at },
      { id: "ses_2", current: false, device: "desktop", client: "Firefox on Linux", location: null, ip: "203.0.113.9", methods: ["passkey"], started_at: at, last_seen_at: at },
    ]} />);
    expect(el.textContent).not.toContain("172.18.0.8");
    expect(el.textContent).toContain("203.0.113.9");
    expect(el.textContent).toContain("password + authenticator app");
    expect(el.textContent).not.toMatch(/\bTOTP\b/);
    expect(rawLeaks(el)).toEqual([]);
  });

  it("the engine's new-sign-in notice follows the same rule (identity/where.py)", () => {
    const me = readFileSync(resolve(__dirname, "../../api/app/routers/identity_me.py"), "utf8");
    expect(me).toMatch(/New sign-in \{sign_in_origin\(seen\['ip'\]\)\}/);
    expect(me).not.toMatch(/New sign-in from \{seen\['ip'\]/);
  });
});

describe("ids and repeats in API sentences", () => {
  it("an id is stripped with its qualifiers, and a bare qualifier left behind is dropped too", () => {
    expect(plainText("run rt_ab12cd:git:315ad114331e in boss-dogfood")).toBe("run in boss-dogfood");
    expect(plainText(":git:315ad114331e")).toBe("");
    expect(plainText("auth.login.succeeded")).not.toMatch(/auth\./);
  });
  it("the same lineage line on the same day reads once, with a count", () => {
    const at = "2026-10-07T22:15:00Z";
    const lines = [{ at, event: "Started a run", detail: "in boss-dogfood" }, { at, event: "Started a run", detail: "in boss-dogfood" },
      { at, event: "Sponsor changed", detail: "" }];
    expect(lineageLines(lines).map((l) => [l.line.event, l.times])).toEqual([["Started a run", 2], ["Sponsor changed", 1]]);
  });
});

describe("a Passport's limits", () => {
  const vera = () => F.passports(NOW).find((p) => p.id === "agt_vera")!;

  it("a locked ability says why in plain words", () => {
    expect(neverDelegableWhy("github:merge", "Authorize merges")).toBe("Agents can never merge. A person does.");
    expect(neverDelegableWhy("user:invite", "Invite people")).toBe("Agents can never invite people. A person does.");
  });

  it("the access limit changes only in Change limit mode, with a preview, and saves the diff", async () => {
    const a = vera();
    await mount(<PassportScreen initial={a} grants={[]} />, ROLE_PERMISSIONS.owner, (b) => ({ ...a, ceiling: (b as { ceiling: string[] }).ceiling }));
    expect(host!.querySelectorAll(".idn-tg:not([aria-disabled='true'])").length).toBe(0);
    await click(q("[data-testid='edit-ceiling']"));
    expect(q("[data-testid='ceiling-preview']")!.textContent).toContain("No changes yet");
    const merge = q("[data-testid='ceil-github:merge']")!;
    expect(merge.getAttribute("aria-disabled")).toBe("true");
    expect(merge.getAttribute("title")).toBe("Agents can never merge. A person does.");
    await click(q("[data-testid='ceil-code_task:steer']"));
    expect(q("[data-testid='ceiling-preview']")!.textContent).toContain("What changes when you save");
    await click(q("[data-testid='save-ceiling']"));
    const patch = calls.find((c) => c.method === "PATCH");
    expect(patch?.url).toBe(`/v1/identity/agents/${a.id}/passport`);
    expect((patch?.body as { ceiling: string[] }).ceiling).not.toContain("code_task:steer");
  });

  it("the GitHub limit mirrors the App's permission set exactly", () => {
    const py = readFileSync(resolve(__dirname, "../../../engine/boss_engine/identity/github_setup.py"), "utf8");
    const block = py.slice(py.indexOf("APP_PERMISSIONS = {"), py.indexOf("}", py.indexOf("APP_PERMISSIONS = {")) + 1);
    const engine = Object.fromEntries([...block.matchAll(/"(\w+)": "(\w+)"/g)].map((m) => [m[1], m[2]]));
    expect(APP_PERMISSIONS).toEqual(engine);
  });

  it("offers nothing above the App, keeps a level already held, and saves only real keys", () => {
    expect(levelOffered("pull_requests", "write", "read")).toBe(true);
    expect(levelOffered("issues", "write", "read")).toBe(false);
    expect(levelOffered("issues", "write", "write")).toBe(true);
    expect(limitChanges({ contents: "read" }, { contents: "read", pull_requests: "write" })).toEqual(["Pull requests: no access → write"]);
    expect(limitBody({ contents: "write", push_refs: "boss/*" }, { contents: "read", issues: "none" })).toEqual({ github: { push_refs: "boss/*", contents: "read" } });
  });

  it("the GitHub limit is edited with a preview and saved through the Passport, and says how the agent reaches GitHub", async () => {
    const a: Passport = { ...vera(), external_ceiling: { github: { contents: "read" } } };
    await mount(<PassportScreen initial={a} grants={[]} />, ROLE_PERMISSIONS.owner,
      (b) => ({ ...a, external_ceiling: (b as { external_ceiling: Passport["external_ceiling"] }).external_ceiling }));
    const how = q("[data-testid='github-how']")!;
    expect(how.textContent).toContain("How this agent reaches GitHub");
    expect([...how.querySelectorAll("a")].map((x) => x.getAttribute("href"))).toEqual(["/?area=setup&focus=repository", "/account/agents"]);
    await click(q("[data-testid='edit-github-limit']"));
    expect((q("[data-testid='gh-issues-write']") as HTMLButtonElement).disabled).toBe(true);
    await click(q("[data-testid='gh-pull_requests-write']"));
    expect(q("[data-testid='github-limit-preview']")!.textContent).toContain("Pull requests: no access → write");
    await click(q("[data-testid='save-github-limit']"));
    expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ external_ceiling: { github: { contents: "read", pull_requests: "write" } } });
    expect(rawLeaks(host!)).toEqual([]);
  });

  it("a Viewer cannot change either limit", async () => {
    await mount(<PassportScreen initial={vera()} grants={[]} />, ROLE_PERMISSIONS.viewer);
    expect(q("[data-testid='edit-ceiling']")!.getAttribute("aria-disabled")).toBe("true");
    expect(q("[data-testid='edit-github-limit']")!.getAttribute("aria-disabled")).toBe("true");
  });
});

describe("People: Manage access on every row", () => {
  it("whoever may assign roles gets a Manage access link per person, straight to their Access tab", async () => {
    const people = F.people(NOW);
    await mount(<PeopleList people={people} roles={F.roles(NOW)} />);
    const p = people[0];
    expect(q(`[data-testid='manage-access-${p.id}']`)?.getAttribute("href")).toBe(`/identity/people/${p.id}?tab=access`);
    expect(host!.querySelectorAll("[data-testid^='manage-access-']").length).toBe(people.length);
  });
  it("a Viewer sees no Manage access", async () => {
    await mount(<PeopleList people={F.people(NOW)} roles={F.roles(NOW)} />, ROLE_PERMISSIONS.viewer);
    expect(host!.querySelectorAll("[data-testid^='manage-access-']").length).toBe(0);
  });
});
